---
name: source-command-latch
description: Choose this filesystem scope's Shared or Private KB. Use for latch, /latch, or re-pinning one scope without changing others.
---

# source-command-latch

Latch installs its runtime once, then scopes KB access by filesystem root. On a
fresh explicit-scope install, an unscoped location is safely LOCKED until the
user explicitly chooses Shared or Private. An upgraded global-KB install instead
reports `compatibility_global` and keeps using the exact previous global KB until
the user deliberately migrates. Descendants inherit the nearest scope. This
command never rewrites the install-level KB pin or copies/imports KB content.

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
bash "$latch_home/bin/latch.sh"
```

Show the current root, state, policy, KB path, and source from the receipt. Then
ask which of these the user intends:

- keep the previous binding when re-latching an UNLATCHED scope;
- create a Shared scope that uses the existing global KB;
- create a Private scope with a fresh separate KB, or bind an existing
  absolute private KB directory.

Before changing state, ask for the exact reply `latch`. Then run exactly one:

```bash
bash "$latch_home/bin/latch.sh" --confirm latch
bash "$latch_home/bin/latch.sh" --confirm latch --shared
bash "$latch_home/bin/latch.sh" --confirm latch --private --kb-dir "/absolute/kb/path"
bash "$latch_home/bin/latch.sh" --confirm latch --private --new-kb
```

If status reports `compatibility_global` and the user explicitly wants every
other unscoped location to become LOCKED, explain the effect and ask for the
same exact `latch` confirmation. Only from a compatibility/Shared location
(never a Private scope), run:

```bash
bash "$latch_home/bin/latch.sh" --confirm latch --shared --require-explicit-scopes
```

This creates the current Shared boundary and changes only the machine's default
for other unscoped locations. Existing explicit scopes and all KB content stay
unchanged.

Show the complete receipt. If the binding changed, tell the user to start a
fresh agent task in this project and not resume the old one; an idempotent
receipt explicitly says no new task is needed.
Do not offer automatic content transfer; a new KB starts clean. If a global
environment override is active, do not claim project separation. Project KB
selection is not a complete NDA clean-room boundary for install-level artifacts.
