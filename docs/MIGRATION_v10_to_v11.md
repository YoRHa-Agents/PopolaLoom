# Migration: PopolaLoom v1.0.x to v1.1.0

<!-- updated: 2026-05-11 -->

v1.1.0 introduces a nested `[user_preferences]` schema. Existing flat
preferences remain readable; when `popolad.toml` is loaded, PopolaLoom writes a
`popolad.toml.v1.bak` backup and migrates the live file to `schema_version = 2`.

## Key map

| v1.0 flat key | v1.1 nested key |
|---|---|
| `default_runtime` | `routing.default_runtime` |
| `default_local_cli` | `routing.default_local_cli` |
| `fallback_chain` | `routing.fallback_chain` |
| `cloud_target_priority` | `routing.cloud_target_priority` |
| `default_cloud_target` | `cursor-cloud.default_cloud_target` |
| `hitl_enabled` | `defaults.hitl_enabled` |
| `follow_devola_flow` | `defaults.follow_devola_flow` |
| `prompt_each_dispatch` | `defaults.prompt_each_dispatch` |

## Dotted writes

```bash
popola init prefs --set cursor-cloud.model=composer-2 \
  --set codex.sandbox=read-only \
  --set lark.notify_on_completed=false
```

Use `popola init prefs show --json` to inspect the raw nested structure, and
`popola doctor` to verify whether preferences are absent, v1 flat/migrated, or
valid v2 nested.

## Dispatch Q&A

`[user_preferences.dispatch] ambiguity_resolution = "prompt"` enables the new
option-group flow when `popola dispatch` is missing key dimensions. Use
`popola dispatch "..." --no-wizard` to retain v1.0 silent-default behavior for
scripted invocations.
