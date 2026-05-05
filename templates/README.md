# popolaloom-mcp config templates

Two templates wire the popolaloom-mcp stdio server into IDE Agents that speak the
Anthropic Model Context Protocol (MCP). Copy whichever matches your host and adjust
the `command` to your interpreter (use the absolute path of the python that has
popolaloom installed if your IDE Agent doesn't inherit your shell `$PATH`).

- **Cursor IDE** — copy `mcp.json` to `~/.cursor/mcp.json` (user-level) or to the
  project's `.cursor/mcp.json` (project-level overrides take precedence). Cursor
  will spawn `python -m popolaloom.mcp` over stdio on startup; you'll then see all
  7 dispatch verbs (`popola_submit`, `popola_list`, `popola_status`,
  `popola_attach_stream`, `popola_supply_feedback`, `popola_cancel`,
  `popola_inject_subtask`) appear in the Agent's tool list.
- **Claude Code** — copy `claude_settings.json` to `~/.claude/settings.json`
  (user-level) or to the project's `.claude/settings.json`. The shape mirrors
  Cursor's but Claude Code expands `${HOME}` directly (no `${env:HOME}` prefix).

After copying, ensure `popolad` is running with `popola popolad start` (Stage A);
verbs return a friendly "popolad not running" error if the daemon isn't up.
