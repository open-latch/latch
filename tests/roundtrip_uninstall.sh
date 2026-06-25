#!/usr/bin/env bash
# tests/roundtrip_uninstall.sh — manual integration test for the latch
# uninstaller + kill switch (the inverse of install_engine). Not a unit test
# (needs the `claude` CLI; mutates a sandbox), so it is NOT collected by pytest.
#
# It installs latch into a fully SANDBOXED Claude Code config, exercises the
# kill switch, uninstalls, and asserts the REAL ~/.claude config + this repo are
# byte-unchanged. See tests/roundtrip_uninstall.md for the full description.
#
# Isolation (this is what keeps the test off your real machine):
#   HOME=<sandbox>             -> settings.json + ~/.claude (Path.home() based)
#   CLAUDE_CONFIG_DIR=<sandbox>-> the `claude` CLI's user-scope MCP registry
#   LATCH_HOME=<sandbox>       -> snippet source, DISABLE files, KB data
#   CLAUDE_KB_HOME=<sandbox>   -> legacy alias coverage
#   CLAUDE_COMMANDS_DIR        -> pins the slash-commands dir for both installers
# Safety gate: a --dry-run must report the SANDBOX settings path or the run
# ABORTS without writing. Worst case is "aborted", never "clobbered real config".
#
# Usage:  bash tests/roundtrip_uninstall.sh [GIT_COMMITTISH]   (default: HEAD)
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COMMIT="${1:-HEAD}"
REAL_HOME="$HOME"
REAL_DOTCLAUDE="$HOME/.claude.json"
REAL_SETTINGS="$HOME/.claude/settings.json"
REAL_CMDS="$HOME/.claude/commands"
VENV_PY="$REPO/.venv/bin/python"; [ -x "$VENV_PY" ] || VENV_PY="$(command -v python3 || command -v python)"

PASS=0; FAIL=0
ok(){ echo "  PASS: $1"; PASS=$((PASS+1)); }
bad(){ echo "  FAIL: $1"; FAIL=$((FAIL+1)); }
# Read the registry FILE directly (deterministic; no `claude mcp get` health-
# check, which spawns the server and is timing-flaky). The user-scope registry
# lives at <config-dir>/.claude.json.
mcp_cmd(){ "$VENV_PY" -c "import json,os,sys;p=sys.argv[1];name=sys.argv[2];d=json.load(open(p)) if os.path.exists(p) else {};print(d.get('mcpServers',{}).get(name,{}).get('command',''))" "$1" "${2:-latch}" 2>/dev/null; }
mcp_present(){ "$VENV_PY" -c "import json,os,sys;p=sys.argv[1];name=sys.argv[2];d=json.load(open(p)) if os.path.exists(p) else {};print(name in d.get('mcpServers',{}))" "$1" "${2:-latch}" 2>/dev/null; }
mcp_args(){ "$VENV_PY" -c "import json,os,sys;p=sys.argv[1];name=sys.argv[2];d=json.load(open(p)) if os.path.exists(p) else {};print(' '.join(d.get('mcpServers',{}).get(name,{}).get('args',[])))" "$1" "${2:-latch}" 2>/dev/null; }
mcp_fingerprint(){ "$VENV_PY" -c "import json,os,sys;p=sys.argv[1];d=json.load(open(p)) if os.path.exists(p) else {};m=d.get('mcpServers',{});print(json.dumps({k:m.get(k) for k in ('latch','claude-kb')},sort_keys=True))" "$1" 2>/dev/null; }
cmds_hash(){ (ls -1 "$1" 2>/dev/null | sort; shasum "$1"/*.md 2>/dev/null) | shasum | awk '{print $1}'; }

real_set_before=$(shasum "$REAL_SETTINGS" 2>/dev/null | awk '{print $1}')
real_mcp_before=$(mcp_fingerprint "$REAL_DOTCLAUDE")
real_cmds_before=$(cmds_hash "$REAL_CMDS")
real_git_before=$(git -C "$REPO" status --porcelain=v1 --untracked-files=no | sort | shasum | awk '{print $1}')
echo "### REAL latch/legacy MCP fingerprint (must stay) : $real_mcp_before"

SROOT=$(mktemp -d -t latchsbx.XXXXXX)
SBX="$SROOT/repo"; H="$SROOT/home"; CFG="$H/.claude"
mkdir -p "$SBX" "$CFG/commands"
git -C "$REPO" archive "$COMMIT" | tar -x -C "$SBX"
[ -f "$SBX/bin/uninstall.sh" ] || { echo "ABORT: $COMMIT has no bin/uninstall.sh"; rm -rf "$SROOT"; exit 2; }

cat > "$CFG/settings.json" <<'JSON'
{
  "theme": "dark",
  "permissions": { "allow": ["Bash(ls:*)", "mcp__other-server__do_thing", "mcp__claude-kb__kb_get"] },
  "hooks": {
    "SessionStart": [ { "hooks": [ { "type": "command", "command": "echo NOT-LATCH-sessionstart" } ] } ],
    "PreToolUse":   [ { "matcher": "Bash", "hooks": [ { "type": "command", "command": "echo unrelated-pretooluse" } ] } ]
  }
}
JSON
printf 'my own custom command, not latch\n' > "$CFG/commands/mycustom.md"

export HOME="$H" CLAUDE_CONFIG_DIR="$CFG" LATCH_HOME="$SBX" CLAUDE_KB_HOME="$SBX" CLAUDE_COMMANDS_DIR="$CFG/commands" LATCH_PYTHON="$VENV_PY" CLAUDE_KB_PYTHON="$VENV_PY"
echo "### sandbox: $SROOT (commit $COMMIT)"

# --- safety gate: dry-run must target the sandbox -------------------------
echo; echo "### safety gate: install_engine.sh --dry-run"
dry=$(bash "$SBX/bin/install_engine.sh" --dry-run 2>&1); echo "$dry" | sed 's/^/    /'
case "$dry" in *"$CFG"*) ok "dry-run targets the sandbox settings.json (isolation confirmed)";;
  *) bad "dry-run did NOT target sandbox — refusing to continue"; rm -rf "$SROOT"; echo "ABORTED"; exit 1;; esac

# --- INSTALL --------------------------------------------------------------
echo; echo "### install_engine.sh"; bash "$SBX/bin/install_engine.sh" 2>&1 | sed 's/^/    /'
echo "### install_commands.sh"; bash "$SBX/bin/install_commands.sh" 2>&1 | tail -2 | sed 's/^/    /'

echo; echo "### post-install assertions"
"$VENV_PY" - "$CFG/settings.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1])); allow=s.get("permissions",{}).get("allow",[]); hooks=s.get("hooks",{})
def chk(c,m):print(("  PASS: " if c else "  FAIL: ")+m)
chk(s.get("theme")=="dark","unrelated theme preserved")
chk("Bash(ls:*)" in allow and "mcp__other-server__do_thing" in allow,"unrelated perms preserved")
chk("mcp__latch" in allow,"server-level mcp__latch perm added")
chk("mcp__claude-kb" in allow,"legacy server-level mcp__claude-kb perm preserved")
ss=[h.get("command","") for g in hooks.get("SessionStart",[]) for h in g.get("hooks",[])]
chk(any("NOT-LATCH" in c for c in ss),"non-latch SessionStart hook preserved")
chk(any("/src/hooks/" in c for c in ss),"latch SessionStart hook added")
chk("PreToolUse" in hooks,"unrelated PreToolUse hook preserved")
for ev in ("UserPromptSubmit","Stop","SessionEnd"): chk(ev in hooks,"latch hook event "+ev+" added")
PY
case "$(mcp_args "$CFG/.claude.json" latch)" in *"$SBX"*) ok "sandbox latch MCP registered -> sandbox repo";; *) bad "sandbox latch MCP missing/wrong";; esac
ls "$CFG/commands"/kb-*.md >/dev/null 2>&1 && ok "latch commands installed in sandbox" || bad "latch commands not installed"
[ "$(mcp_fingerprint "$REAL_DOTCLAUDE")" = "$real_mcp_before" ] && ok "REAL registry unchanged by install (no leak)" || bad "REAL registry changed by install"

# --- KILL SWITCH (all under sandbox KB_HOME) ------------------------------
echo; echo "### kill switch: disable -> status -> enable"
disabled(){ "$VENV_PY" -c "import sys;sys.path.insert(0,'$SBX/src');import paths;print(int(paths.is_disabled()),int(paths.is_write_disabled()))"; }
bash "$SBX/bin/latch_disable.sh" 2>&1 | sed 's/^/    /'
[ -f "$SBX/DISABLE" ] && ok "latch_disable created DISABLE sentinel" || bad "DISABLE not created"
[ "$(disabled)" = "1 1" ] && ok "is_disabled()+is_write_disabled() true after full disable (write implies full)" || bad "kill-switch state wrong after full disable: $(disabled) (want '1 1')"
# capture-then-match: piping into `grep -q` would let grep close the pipe on
# first match, SIGPIPE the status script's next echo, and (under pipefail) read
# as a false failure.
st=$(bash "$SBX/bin/latch_status.sh" 2>&1); case "$st" in *DISABLED*) ok "latch_status reports DISABLED";; *) bad "status not DISABLED";; esac
bash "$SBX/bin/latch_enable.sh" 2>&1 | sed 's/^/    /'
[ ! -f "$SBX/DISABLE" ] && [ "$(disabled)" = "0 0" ] && ok "latch_enable cleared DISABLE" || bad "enable did not clear DISABLE"
bash "$SBX/bin/latch_disable.sh" --write-only >/dev/null 2>&1
[ -f "$SBX/DISABLE_WRITE" ] && [ "$(disabled)" = "0 1" ] && ok "--write-only sets DISABLE_WRITE (reads stay live)" || bad "write-only state wrong: $(disabled)"
bash "$SBX/bin/latch_enable.sh" --all >/dev/null 2>&1
[ ! -f "$SBX/DISABLE_WRITE" ] && [ "$(disabled)" = "0 0" ] && ok "latch_enable --all cleared DISABLE_WRITE" || bad "enable --all left a sentinel"
[ ! -f "$REPO/DISABLE" ] && [ ! -f "$REPO/DISABLE_WRITE" ] && ok "no kill-switch file leaked into the real repo" || bad "kill-switch file LEAKED into real repo!"

# --- UNINSTALL ------------------------------------------------------------
echo; echo "### uninstall.sh --yes"; bash "$SBX/bin/uninstall.sh" --yes 2>&1 | sed 's/^/    /'
echo "### uninstall.sh --check"; bash "$SBX/bin/uninstall.sh" --check; UNINS_RC=$?
[ $UNINS_RC -eq 0 ] && ok "uninstall --check clean (rc=0)" || bad "uninstall --check rc=$UNINS_RC"

echo; echo "### post-uninstall assertions"
"$VENV_PY" - "$CFG/settings.json" <<'PY'
import json,sys
s=json.load(open(sys.argv[1])); allow=s.get("permissions",{}).get("allow",[]); hooks=s.get("hooks",{})
def chk(c,m):print(("  PASS: " if c else "  FAIL: ")+m)
chk(s.get("theme")=="dark","unrelated theme preserved")
chk("Bash(ls:*)" in allow and "mcp__other-server__do_thing" in allow,"unrelated perms preserved")
chk("mcp__latch" not in allow and not any(str(r).startswith("mcp__latch__") for r in allow),"primary latch perms stripped")
chk("mcp__claude-kb" not in allow and not any(str(r).startswith("mcp__claude-kb__") for r in allow),"legacy latch perms stripped")
ss=[h.get("command","") for g in hooks.get("SessionStart",[]) for h in g.get("hooks",[])]
chk(any("NOT-LATCH" in c for c in ss),"non-latch SessionStart hook preserved")
chk(not any("/src/hooks/" in c for c in ss),"latch SessionStart hook removed")
chk("PreToolUse" in hooks,"unrelated PreToolUse hook preserved")
for ev in ("UserPromptSubmit","Stop","SessionEnd"): chk(ev not in hooks,"latch hook event "+ev+" removed")
PY
[ "$(mcp_present "$CFG/.claude.json" latch)" = "False" ] && [ "$(mcp_present "$CFG/.claude.json" claude-kb)" = "False" ] && ok "sandbox MCP deregistered" || bad "sandbox MCP still present"
ls "$CFG/commands"/kb-*.md >/dev/null 2>&1 && bad "latch commands NOT removed" || ok "latch commands removed"
[ -f "$CFG/commands/mycustom.md" ] && ok "user command PRESERVED (not clobbered)" || bad "user command removed!"

# --- REAL STATE UNTOUCHED -------------------------------------------------
echo; echo "### REAL state verification"
[ "$real_set_before" = "$(shasum "$REAL_SETTINGS" 2>/dev/null | awk '{print $1}')" ] && ok "REAL settings.json unchanged" || bad "REAL settings.json CHANGED"
[ "$real_mcp_before" = "$(mcp_fingerprint "$REAL_DOTCLAUDE")" ] && ok "REAL latch/legacy MCP registration unchanged" || bad "REAL MCP registration CHANGED"
[ "$real_cmds_before" = "$(cmds_hash "$REAL_CMDS")" ] && ok "REAL ~/.claude/commands unchanged" || bad "REAL commands dir CHANGED"
[ "$real_git_before" = "$(git -C "$REPO" status --porcelain=v1 --untracked-files=no | sort | shasum | awk '{print $1}')" ] && ok "REAL repo tracked files unchanged" || bad "REAL repo tracked files CHANGED"

rm -rf "$SROOT"
echo; echo "### RESULT: $PASS passed, $FAIL failed"
[ $FAIL -eq 0 ] && echo "### ALL GREEN — install/uninstall + kill switch reversible; this repo untouched." || { echo "### SOME CHECKS FAILED."; exit 1; }
