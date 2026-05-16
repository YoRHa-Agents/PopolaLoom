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
