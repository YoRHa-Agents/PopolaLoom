"""PopolaLoom bundled-skills package marker.

Empty by design: the canonical SKILL.md (and any future per-IDE
adapter assets) is shipped as wheel data under the ``popolaloom.skills``
import path so ``importlib.resources.files('popolaloom').joinpath(
'skills', 'popolaloom', 'SKILL.md')`` resolves both in editable / source
checkouts and in installed wheels.

See :mod:`popolaloom.cli._skill_source` for the resolver contract that
``popola init`` uses to copy this SKILL.md into per-IDE install
targets (Cursor / Claude / Copilot / Codex).
"""
