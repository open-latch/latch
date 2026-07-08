---
name: source-command-unlatch
description: Confirmed toggle for latch Unlatched mode. Use when the user invokes $source-command-unlatch, unlatch, /unlatch, or wants to latch/re-latch the current install.
---

# source-command-unlatch

Use this skill when the user wants to turn latch's project-judgment layer off,
compare against vanilla agent behavior, or re-latch after using the escape
hatch.

Unlatched mode is in-place and install-level. It is not a controlled benchmark
harness, uninstall, cloud flow, telemetry flow, or disposable project clone. It
uses latch's existing full-disable sentinel plus an UNLATCHED receipt so future
hooks show that latch influence is off for this latch install until the user
re-latches. If the user changes repos before re-latching, latch remains off and
should say so loudly. It also masks latch's managed `CLAUDE.md` / `AGENTS.md`
regions in the current project/ancestor files while unlatched, then restores
them when the user re-latches.

## Command Template

First locate the latch checkout:

```bash
latch_home="${LATCH_HOME:-}"
if [ -z "$latch_home" ] && [ -n "${CLAUDE_KB_HOME:-}" ]; then
  latch_home="$CLAUDE_KB_HOME"
fi
if [ -z "$latch_home" ]; then
  search_dir="$PWD"
  while [ "$search_dir" != "/" ]; do
    for instruction_file in "$search_dir/AGENTS.md" "$search_dir/CLAUDE.md"; do
      if [ -f "$instruction_file" ]; then
        latch_home="$(sed -n 's|^UNLATCHED_LATCH_HOME=\(.*\)$|\1|p' "$instruction_file" | head -n 1)"
        [ -n "$latch_home" ] && break
        latch_home="$(sed -n 's|.*Follow `\([^`]*\)/README\.md` per-user setup.*|\1|p' "$instruction_file" | head -n 1)"
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

Then inspect state:

```bash
bash "$latch_home/bin/unlatch.sh"
```

Never run a state-changing command immediately, even in dangerous/yolo
permission modes.

If the output says LATCHED, ask:

```text
Latch is currently LATCHED.

Switch to UNLATCHED mode?
This turns latch's project-judgment layer off for this latch install, masks latch-managed CLAUDE.md/AGENTS.md regions in this project, and leaves KB data intact.
Latch remains off for this latch install, even if you change repos, until you re-latch.
To re-latch later, run /unlatch again.

Reply exactly: unlatch
```

If the output says UNLATCHED, ask:

```text
Latch is currently UNLATCHED.

Switch back to LATCHED mode?
Latch hooks will resume on the next prompt unless LATCH_UNLATCHED is set.
If LATCH_UNLATCHED is set, unset it too.

Reply exactly: latch
```

Stop there. Only after the user replies exactly `unlatch`, run:

```bash
bash "$latch_home/bin/unlatch.sh" --confirm unlatch
```

Only after the user replies exactly `latch`, run:

```bash
bash "$latch_home/bin/unlatch.sh" --confirm latch
```

Show the command output plainly. If latch is unlatched, say: "Latch is currently
UNLATCHED. Run /unlatch to re-latch. If LATCH_UNLATCHED is set, unset it too."
If latch is latched again, say latch hooks resume on the next prompt unless an
environment disable flag remains set. Do not call it a controlled benchmark or
statistical A/B test.
