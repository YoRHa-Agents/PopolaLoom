"""Fast docs contract tests for the v0.8.2 web remediation pass."""

from __future__ import annotations

import json
import re
from pathlib import Path

import popolaloom

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _front_matter(path: Path) -> dict[str, str]:
    text = _read(path)
    lines = text.splitlines()
    assert lines[0] == "---", f"{path} missing opening front matter fence"
    end = lines.index("---", 1)
    data: dict[str, str] = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = value.strip().strip('"')
    return data


def test_docs_config_version_matches_package_version() -> None:
    config = _read(DOCS / "_config.yml")
    match = re.search(r'^popola_version:\s+"([^"]+)"$', config, flags=re.MULTILINE)
    assert match, "docs/_config.yml must define popola_version"
    assert match.group(1) == popolaloom.__version__


def test_landing_i18n_keys_are_covered_by_en_and_zh_dicts() -> None:
    en = json.loads(_read(DOCS / "assets" / "i18n" / "en.json"))
    zh = json.loads(_read(DOCS / "assets" / "i18n" / "zh.json"))
    assert set(en) == set(zh)

    surfaces = [
        DOCS / "index.md",
        DOCS / "_includes" / "header.html",
        DOCS / "_includes" / "footer.html",
    ]
    keys: set[str] = set()
    for path in surfaces:
        keys.update(re.findall(r'data-i18n="([^"]+)"', _read(path)))

    missing = keys - set(en)
    assert not missing, f"missing i18n dictionary keys: {sorted(missing)}"


def test_i18n_runtime_supports_flat_keys_and_localized_routes() -> None:
    script = _read(DOCS / "assets" / "js" / "i18n.js")
    assert "Object.prototype.hasOwnProperty.call(dict, key)" in script
    assert "dataset.translationUrl" in script
    assert "window.location.href = TRANSLATION_URL" in script


def test_main_docs_have_chinese_counterparts_and_backlinks() -> None:
    pairs = {
        "QUICKSTART.md": "/zh/QUICKSTART.html",
        "USER_GUIDE.md": "/zh/USER_GUIDE.html",
        "DEMO.md": "/zh/DEMO.html",
    }
    for english_name, zh_url in pairs.items():
        english = DOCS / english_name
        zh = DOCS / "zh" / english_name

        english_meta = _front_matter(english)
        zh_meta = _front_matter(zh)

        assert english_meta["lang"] == "en"
        assert english_meta["translation_url"] == zh_url
        assert zh_meta["lang"] == "zh"
        assert zh_meta["translation_url"] == f"/{english_name.removesuffix('.md')}.html"


def test_demo_page_is_linked_and_explains_design_flow() -> None:
    index = _read(DOCS / "index.md")
    header = _read(DOCS / "_includes" / "header.html")
    demo = _read(DOCS / "DEMO.md")

    assert "DEMO.html" in index
    assert "DEMO.html" in header
    for marker in (
        "## What this demo proves",
        "## Design and implementation flow",
        "## Hands-off envelope walkthrough",
        "## HITL walkthrough",
    ):
        assert marker in demo


def test_primary_user_facing_docs_have_no_stale_placeholder_markers() -> None:
    forbidden = ("v0.8.1", "0.8.1", "placeholder", "not yet wired", "scaffolded")
    paths = [
        REPO_ROOT / "README.md",
        DOCS / "index.md",
        DOCS / "QUICKSTART.md",
        DOCS / "USER_GUIDE.md",
        DOCS / "DEMO.md",
        DOCS / "_layouts" / "default.html",
        DOCS / "_includes" / "footer.html",
        DOCS / "assets" / "i18n" / "en.json",
        DOCS / "assets" / "i18n" / "zh.json",
    ]
    offenders: list[str] = []
    for path in paths:
        lowered = _read(path).lower()
        for marker in forbidden:
            if marker in lowered:
                offenders.append(f"{path.relative_to(REPO_ROOT)} contains {marker!r}")
    assert not offenders, "\n".join(offenders)
