---
name: source-command-unlatch
description: Confirmed scope-local toggle for Latch Unlatched mode. Use for unlatch, /unlatch, or turning Latch off for the current filesystem scope.
---

# source-command-unlatch

Unlatch is scope-local. It turns off Latch guidance, tools, hooks, maintenance,
and automatic writes for the current scope. Descendants and any authorized root
aliases of that same scope follow its mode; other scopes and all KBs are
unchanged. It does not uninstall Latch, delete/copy/import KB content, or change
the install-level KB pin.

First locate the full Latch checkout. Prefer `LATCH_HOME`, then
`CLAUDE_KB_HOME`, then this installed skill's baked-in checkout, then the
`Details: .../docs/agent-contract-reference.md` line in an ancestor `AGENTS.md`
or `CLAUDE.md`, then the current Git root when it contains `src/mcp_server.py`
and `commands/`. Refuse the action if the checkout cannot be proved.

```bash
latch_home="${LATCH_HOME:-}"
if [ -z "$latch_home" ] && [ -n "${CLAUDE_KB_HOME:-}" ]; then
  latch_home="$CLAUDE_KB_HOME"
fi
installed_latch_home=__LATCH_INSTALLED_HOME__
if [ -z "$latch_home" ] && [ -f "$installed_latch_home/src/mcp_server.py" ]; then
  latch_home="$installed_latch_home"
fi
if [ -z "$latch_home" ]; then
  search_dir="$PWD"
  while [ "$search_dir" != "/" ]; do
    for instruction_file in "$search_dir/AGENTS.md" "$search_dir/CLAUDE.md"; do
      if [ -f "$instruction_file" ]; then
        latch_home="$(sed -n 's|.*Details: \(.*\)/docs/agent-contract-reference\.md\..*|\1|p' "$instruction_file" | head -n 1)"
        [ -n "$latch_home" ] && break
      fi
    done
    [ -n "$latch_home" ] && break
    search_dir="$(dirname "$search_dir")"
  done
fi
if [ -z "$latch_home" ]; then
  candidate="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
  if [ -f "$candidate/src/mcp_server.py" ] && [ -d "$candidate/commands" ]; then
    latch_home="$candidate"
  fi
fi
if [ -z "$latch_home" ] || [ ! -f "$latch_home/src/mcp_server.py" ]; then
  echo "Could not find latch checkout; set LATCH_HOME to your latch install." >&2
  exit 1
fi
```

Inspect without mutation:

```bash
bash "$latch_home/bin/unlatch.sh"
```

If this project is LATCHED, ask:

```text
Latch is currently LATCHED for this project.

Switch this filesystem scope to UNLATCHED mode?
Its descendants and authorized aliases follow the same mode.
Other scopes and every KB remain unchanged.

Reply exactly: unlatch
```

If this project is UNLATCHED, ask:

```text
Latch is currently UNLATCHED for this project.

Switch this project back to LATCHED mode using its previous KB binding?

Reply exactly: latch
```

Never mutate immediately. Only after the exact reply, run one command:

```bash
bash "$latch_home/bin/unlatch.sh" --confirm unlatch
bash "$latch_home/bin/latch.sh" --confirm latch
```

Show the complete receipt. If the mode changed, tell the user to start a fresh
agent task in this project and not resume the old one so the instruction mask
takes effect; an idempotent receipt explicitly says no new task is needed. If an install-wide
legacy sentinel or environment override is active, do not claim project separation; follow the command's
recovery guidance. To select a separate or existing KB, use the `latch` command
skill instead. Warn that temporary managed instruction-file edits may appear in
Git and should not be committed; `/latch` restores them. Project-local mode does
not claim a complete NDA clean room for install-level artifacts.
