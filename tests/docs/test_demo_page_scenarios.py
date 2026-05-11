from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"

SCENARIOS = (
    "cli-preferences-wizard",
    "multi-cli-relay",
    "daemon-doctor-fix",
)

DELIVERABLE_CLASSES = (
    "event-sequence",
    "pitfalls",
    "verification-command",
    "skill-workflow-link",
)

EN_LABELS = (
    "Expected event sequence",
    "Common pitfalls",
    "Verification command",
    "Skill / Workflow link",
)

ZH_LABELS = (
    "预期事件序列",
    "常见坑",
    "验证命令",
    "Skill / Workflow 链接",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _section(text: str, scenario_id: str) -> str:
    match = re.search(
        rf'<section id="{re.escape(scenario_id)}".*?</section>',
        text,
        flags=re.DOTALL,
    )
    assert match is not None, f"missing section for {scenario_id}"
    return match.group(0)


def test_new_demo_scenarios_exist_in_english_and_chinese() -> None:
    english = _read(DOCS / "demo-page.md")
    chinese = _read(DOCS / "zh" / "demo-page.md")

    for scenario_id in SCENARIOS:
        assert f'href="#{scenario_id}"' in english
        assert f'id="{scenario_id}"' in english
        assert f'href="#{scenario_id}"' in chinese
        assert f'id="{scenario_id}"' in chinese


def test_new_english_scenarios_have_required_deliverables() -> None:
    text = _read(DOCS / "demo-page.md")

    for scenario_id in SCENARIOS:
        section = _section(text, scenario_id)
        for label in EN_LABELS:
            assert label in section, f"{scenario_id} missing {label!r}"
        for class_name in DELIVERABLE_CLASSES:
            assert class_name in section, f"{scenario_id} missing {class_name!r}"


def test_new_chinese_scenarios_have_required_deliverables() -> None:
    text = _read(DOCS / "zh" / "demo-page.md")

    for scenario_id in SCENARIOS:
        section = _section(text, scenario_id)
        for label in ZH_LABELS:
            assert label in section, f"{scenario_id} missing {label!r}"
        for class_name in DELIVERABLE_CLASSES:
            assert class_name in section, f"{scenario_id} missing {class_name!r}"


def test_new_demo_scenarios_use_progressive_command_details() -> None:
    for relative in ("demo-page.md", "zh/demo-page.md"):
        text = _read(DOCS / relative)
        for scenario_id in SCENARIOS:
            section = _section(text, scenario_id)
            assert "<details" in section
            assert "aria-describedby" in section
