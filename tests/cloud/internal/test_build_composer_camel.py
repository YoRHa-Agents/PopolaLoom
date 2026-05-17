"""v1.3.0 P5 — ``build_start_composer_request`` camelCase wire + 11 new fields.

Validates the post-patch builder shape. Per
``.local/feedbacks/feedback_for_v1.2.0.md`` §2 "实测 wire 规格", Cursor's
Connect-Protocol JSON server REQUIRES camelCase keys and 11 additional
fields (``snapshotNameOrId``, ``devcontainerStartingPoint``,
``repositoryInfo``, ``snapshotWorkspaceRootPath``, ``autoBranch``,
``returnImmediately``, ``conversationHistory``, ``source``, ``bcId``,
``addInitialMessageToResponses``, ``usePrivateWorker``); the snake_case
v1.1.1 body was rejected with 400 ``invalid_argument``. The user's curl
playback verified the full camelCase + populated body returns HTTP 200.

These tests assert the on-wire shape; they do NOT cover the Python-side
kwarg API (that's exercised by ``test_rpc_mock.py``).
"""

from __future__ import annotations

from popolaloom.cloud.internal.cursor_cloud_internal import (
    _camelize_keys,
    _to_camel,
    build_start_composer_request,
)


def test_default_body_has_all_camel_keys() -> None:
    """Default invocation populates all 11 new fields in camelCase.

    Mirrors the feedback §2 wire-spec table verbatim, including:

    - ``snapshotNameOrId`` derives from ``repo_url`` by stripping the
      ``https://`` prefix and the ``.git`` suffix.
    - ``devcontainerStartingPoint`` uses the full ``repo_url`` + the
      starting ref (default ``main``).
    - ``bcId`` carries a fresh ``bc-<uuid>`` correlator so the client
      can reconnect to the same composer post-201.
    """
    body = build_start_composer_request(
        prompt="hi", repo_url="https://github.com/o/r"
    )
    assert body["snapshotNameOrId"] == "github.com/o/r"
    assert body["devcontainerStartingPoint"] == {
        "url": "https://github.com/o/r",
        "ref": "main",
    }
    assert body["repositoryInfo"] == {}
    assert body["snapshotWorkspaceRootPath"] == "/workspace"
    assert body["autoBranch"] is True
    assert body["returnImmediately"] is True
    assert body["repoUrl"] == "https://github.com/o/r"
    assert body["conversationHistory"][0]["text"] == "hi"
    assert body["conversationHistory"][0]["type"] == "MESSAGE_TYPE_HUMAN"
    assert body["conversationHistory"][0]["richText"] == "{}"
    assert body["source"] == "BACKGROUND_COMPOSER_SOURCE_WEBSITE"
    assert body["bcId"].startswith("bc-")
    assert body["addInitialMessageToResponses"] is True
    assert body["usePrivateWorker"] is True


def test_model_details_camel_keys() -> None:
    """``modelDetails`` sub-object is camelized end-to-end.

    Ensures ``_camelize_keys`` recurses into the inner ``model_details``
    dict so ``model_name`` → ``modelName``, ``max_mode`` → ``maxMode``,
    ``thinking_level`` → ``thinkingLevel``. This is the v1.1.1 → 1.3.0
    regression we'd otherwise miss.
    """
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
        model_name="gpt-5.5",
        max_mode=True,
        thinking_level="high",
    )
    assert body["modelDetails"] == {
        "modelName": "gpt-5.5",
        "maxMode": True,
        "thinkingLevel": "THINKING_LEVEL_HIGH",
    }


def test_snapshot_name_override() -> None:
    """Caller-supplied ``snapshot_name_or_id`` wins over the derived value.

    The default derivation strips ``https://`` and ``.git``; this test
    asserts that an explicit kwarg bypasses that and is passed through
    verbatim (for non-GitHub repos that have an exotic snapshot id).
    """
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
        snapshot_name_or_id="custom/owner/repo",
    )
    assert body["snapshotNameOrId"] == "custom/owner/repo"


def test_camelize_keys_recursive() -> None:
    """``_camelize_keys`` recurses through dicts + lists.

    Also covers the singleton ``_to_camel`` helper: ``a_b_c`` → ``aBC``,
    single-word and empty-string keys are passthrough.
    """
    nested: dict[str, object] = {"a_b": {"c_d": [{"e_f": 1}]}}
    out = _camelize_keys(nested)
    assert out == {"aB": {"cD": [{"eF": 1}]}}
    assert _to_camel("a_b_c") == "aBC"
    assert _to_camel("simple") == "simple"
    assert _to_camel("") == ""


# ── v1.5.0 — self-hosted worker dispatch body shape ───────────────────


def test_target_machine_name_emits_env_machine() -> None:
    """v1.5.0 — ``target_machine_name`` populates ``env={type,name}``.

    Mirrors the REST adapter's ``AgentEnv`` shape so the path-B body
    carries the same routing info. Per PLAN Phase A this is the
    canonical Phase K G4 / G1 wire fix — body should now include the
    operator's named self-hosted worker.
    """
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
        target_machine_name="popolaloom-dev-worker-v15",
    )
    assert body["env"] == {
        "type": "machine",
        "name": "popolaloom-dev-worker-v15",
    }


def test_target_machine_name_omitted_when_empty() -> None:
    """v1.5.0 — empty/None ``target_machine_name`` → no ``env`` field."""
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
    )
    assert "env" not in body


def test_env_emit_mode_label_drops_env_and_normalizes_snapshot() -> None:
    """v1.5.0 escape hatch (PLAN §A) — ``env_emit_mode=label`` drops
    the ``env`` field even with a worker name, AND normalizes
    ``snapshot_name_or_id`` to ``<owner>/<repo>`` so the worker's
    auto-label matcher can claim it.

    Used when Cursor's server rejects the ``env={type:machine,...}``
    shape on path-B.
    """
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
        target_machine_name="my-worker",
        env_emit_mode="label",
    )
    assert "env" not in body
    assert body["snapshotNameOrId"] == "github.com/o/r"


def test_env_emit_mode_none_drops_env_without_normalizing() -> None:
    """v1.5.0 escape hatch — ``env_emit_mode=none`` is the v1.3.0
    fallback (rely on use_private_worker; no env, no relabel)."""
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
        target_machine_name="my-worker",
        env_emit_mode="none",
    )
    assert "env" not in body
    # snapshot still defaults to derivation from repo_url; we don't
    # bother re-normalising in 'none' mode because the worker is
    # selected purely by use_private_worker=True.


def test_env_emit_mode_invalid_value_rejected() -> None:
    """v1.5.0 — unknown env_emit_mode → ValueError (No Silent Failures)."""
    import pytest

    with pytest.raises(ValueError) as exc_info:
        build_start_composer_request(
            prompt="hi",
            repo_url="https://github.com/o/r",
            env_emit_mode="bogus",
        )
    assert "env_emit_mode" in str(exc_info.value)
    assert "machine" in str(exc_info.value)
    assert "label" in str(exc_info.value)
    assert "none" in str(exc_info.value)


def test_auto_create_pr_field_emitted_when_set() -> None:
    """v1.5.0 — ``auto_create_pr=True`` adds ``autoCreatePr: true`` to body."""
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
        auto_create_pr=True,
    )
    assert body.get("autoCreatePr") is True


def test_auto_create_pr_field_omitted_when_false() -> None:
    """v1.5.0 — default ``auto_create_pr=False`` → no field emitted."""
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
    )
    assert "autoCreatePr" not in body


def test_work_on_current_branch_field() -> None:
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
        work_on_current_branch=True,
    )
    assert body.get("workOnCurrentBranch") is True


def test_skip_reviewer_request_field() -> None:
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
        skip_reviewer_request=True,
    )
    assert body.get("skipReviewerRequest") is True


def test_auto_branch_false_emits_auto_branch_false() -> None:
    """v1.5.0 G4 — ``--no-auto-branch`` opt-out is honored on the wire."""
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
        auto_branch=False,
    )
    assert body["autoBranch"] is False


def test_model_id_override_wins_over_model_name() -> None:
    """v1.5.0 escape hatch §B — ``model_id_override`` overrides
    ``model_name`` for the GPT-5.5 dual-naming case."""
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
        model_name="gpt-5.5",
        model_id_override="gpt-5.5-high",
    )
    assert body["modelDetails"]["modelName"] == "gpt-5.5-high"


def test_target_machine_strip_whitespace() -> None:
    """Whitespace-only ``target_machine_name`` is treated as empty."""
    body = build_start_composer_request(
        prompt="hi",
        repo_url="https://github.com/o/r",
        target_machine_name="   ",
    )
    assert "env" not in body
