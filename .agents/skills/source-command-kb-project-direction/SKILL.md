---
name: source-command-kb-project-direction
description: Show latch's project-direction receipt in Codex. Use when the user invokes $source-command-kb-project-direction, asks for kb-project-direction/project direction/current direction in Codex, or wants the Codex equivalent of Claude Code's /kb-project-direction command.
---

# source-command-kb-project-direction

Use this skill when the user wants the migrated source command
`kb-project-direction` from Codex.

## Command Template

Show latch's minimal project-direction report for the current project. This is a
read-only receipt over existing KB workstreams, focus, governing decisions,
backlog/open items, progress, artifact evidence, and recent unanchored rows.

Prefer the MCP tool because it is the Codex-native surface:

1. Call `kb_project_direction` with a small default limit:
   - `limit=3`
   - `member_limit=20`
   - `unanchored_limit=5`
   - If the user supplied a positive integer, use it as `limit`.

2. Render a concise **Latch project direction** block:
   - Say latch assembled the report from local KB workstreams.
   - Include the receipt `summary` and `why_it_matters`.
   - For each workstream, show id, title, status, objective, and next action.
   - Show governing decisions with id, title, status, authority tier, and relation.
   - Show backlog/open items and recent progress when present.
   - Show artifact coordinates when present.
   - Show `unanchored_evidence` when present, and call it a prompt for
     user-confirmed anchoring rather than automatic backfill.

3. Do not invent missing workstreams or next actions. If no workstreams are
   returned, say that plainly and suggest creating a `kind="workstream"` node or
   setting focus with `/kb-focus set <workstream_id>`.

4. If the `kb_project_direction` MCP tool is not available because the MCP server
   has not restarted or the tool schema is not loaded, fall back to the shell
   wrapper from the latch repo:

   ```bash
   bash /path/to/latch/bin/latch_direction.sh
   ```

   Add `--limit N` when the user requested a limit. On Windows, use:

   ```powershell
   C:\path\to\latch\bin\latch_direction.ps1
   ```

5. Be explicit about the surface:
   - Codex uses this skill, the `kb_project_direction` MCP tool, or the shell wrapper.
   - Claude Code uses `/kb-project-direction`.
   - Do not tell the user that `/kb-project-direction` should appear as a Codex app
     slash command.
