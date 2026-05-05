"""PopolaLoom canonical skill package — wheel-bundled SKILL.md home.

Stage S3 of v0.5.0 (closes the v0.4.1 → v0.5.0 plan §4 Stage S3).
Empty marker so the directory ships as a regular Python package; the
actual skill payload is:

* ``SKILL.md`` — canonical skill content (~ 2800 tokens / ~ 11 KB)
  resolved by :func:`popolaloom.cli._skill_source.canonical_source_path`
  and copied into per-IDE install targets by ``popola init``.
* ``.popolaloom-version`` — plain-text version marker (mirrors
  DevolaFlow's ``.devola-flow-version``); used by ``popola doctor``
  (Stage S4) to detect drift between the installed skill and the
  running wheel.
"""
